import { useMemo, useReducer, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { AlertCircle, BarChart3, FileSpreadsheet, Loader2, RefreshCw } from "lucide-react";

import {
  api,
  type WorkbookCapexTable,
  type WorkbookModelResponse,
} from "../lib/api";
import {
  buildCapexChartModel,
  buildCapexTableView,
  buildHbmTableView,
  capexInteractionReducer,
  capexSeriesOpacity,
  createCapexInteractionState,
  displayWorkbookCell,
  type WorkbookTableView,
  visibleCapexSeries,
  workbookViewState,
} from "../lib/workbookModel";


type WorkbookTab = "hbm" | "capex";

const SERIES_COLORS = ["#2563eb", "#7c3aed", "#059669", "#dc2626", "#d97706", "#0891b2", "#4f46e5"];


function WorkbookGrid({ view }: { view: WorkbookTableView }) {
  return (
    <div className="overflow-hidden rounded-xl border bg-background shadow-sm">
      <div className="border-b bg-muted/20 px-4 py-3">
        <h3 className="text-sm font-bold text-foreground">{view.title}</h3>
      </div>
      <div className="max-h-[68vh] overflow-auto">
        <table className="min-w-[980px] w-full border-collapse text-xs">
          <thead className="sticky top-0 z-30 bg-slate-100/95 shadow-sm backdrop-blur dark:bg-slate-900/95">
            <tr>
              {view.headers.map((header, index) => (
                <th
                  key={`${header}-${index}`}
                  className={`border-b border-r px-3 py-2.5 font-semibold last:border-r-0 ${index === 0 ? "sticky left-0 z-40 min-w-72 bg-slate-100 text-left dark:bg-slate-900" : index >= view.numericColumnStart ? "min-w-28 text-right tabular-nums" : "min-w-44 text-left"}`}
                >
                  {header}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {view.rows.map((row) => (
              <tr
                key={row.key}
                className={row.style === "section"
                  ? "border-b bg-blue-50/70 font-semibold dark:bg-blue-950/30"
                  : row.style === "summary"
                    ? "border-b border-t-2 bg-muted/60 font-semibold"
                    : "border-b odd:bg-muted/10 hover:bg-muted/30"}
              >
                {row.cells.map((cell, index) => (
                  <td
                    key={cell.address}
                    className={`border-r px-3 py-2 last:border-r-0 ${index === 0 ? "sticky left-0 z-10 min-w-72 bg-inherit text-left font-medium" : index >= view.numericColumnStart ? "text-right tabular-nums" : "text-left text-muted-foreground"} ${cell.is_percentage ? "text-blue-700 dark:text-blue-300" : ""}`}
                  >
                    {displayWorkbookCell(cell)}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}


function formatMillions(value: number): string {
  return value.toLocaleString(undefined, { minimumFractionDigits: 0, maximumFractionDigits: 2 });
}


function CapexBarChart({ capex }: { capex: WorkbookCapexTable }) {
  const [interaction, dispatch] = useReducer(capexInteractionReducer, undefined, createCapexInteractionState);
  const [tooltip, setTooltip] = useState<{ seriesName: string; year: number | string | null; value: number } | null>(null);
  const visibleSeries = useMemo(() => visibleCapexSeries(capex.series, interaction), [capex.series, interaction]);
  const model = useMemo(() => buildCapexChartModel(capex.years, visibleSeries), [capex.years, visibleSeries]);
  const width = 1180;
  const height = 500;
  const margin = { top: 34, right: 22, bottom: 74, left: 100 };
  const plotWidth = width - margin.left - margin.right;
  const plotHeight = height - margin.top - margin.bottom;
  const range = model.yMax - model.yMin || 1;
  const y = (value: number) => margin.top + ((model.yMax - value) / range) * plotHeight;
  const zeroY = y(0);
  const groupWidth = plotWidth / Math.max(model.years.length, 1);
  const groupPadding = Math.min(22, groupWidth * 0.12);
  const barWidth = Math.max(3, (groupWidth - groupPadding * 2) / Math.max(model.series.length, 1));
  const ticks = Array.from({ length: 6 }, (_, index) => model.yMax - (range * index) / 5);
  const colorForSeries = (seriesName: string) => {
    const index = capex.series.findIndex((series) => series.name === seriesName);
    return SERIES_COLORS[Math.max(index, 0) % SERIES_COLORS.length];
  };
  const clearHighlight = () => {
    dispatch({ type: "highlight", seriesName: null });
    setTooltip(null);
  };

  return (
    <section className="rounded-lg border bg-background p-4 shadow-sm">
      <div className="mb-2 flex items-center gap-2">
        <BarChart3 className="h-4 w-4 text-primary" />
        <h3 className="text-sm font-bold">Datacentre Capex by Company</h3>
        <span className="text-xs text-muted-foreground">USD million · positive magnitudes</span>
      </div>
      <div className="mb-3 flex flex-wrap items-center gap-1.5 text-[11px]" aria-label="Toggle companies">
        {capex.series.map((series) => {
          const visible = !interaction.hiddenSeries.includes(series.name);
          return (
            <button
              key={series.name}
              type="button"
              aria-pressed={visible}
              aria-label={`${visible ? "Hide" : "Show"} ${series.name.replace(" US Equity", "")}`}
              onClick={() => {
                dispatch({ type: "toggle", seriesName: series.name });
                clearHighlight();
              }}
              onMouseEnter={() => visible && dispatch({ type: "highlight", seriesName: series.name })}
              onMouseLeave={clearHighlight}
              onFocus={() => visible && dispatch({ type: "highlight", seriesName: series.name })}
              onBlur={clearHighlight}
              className={`inline-flex items-center gap-1.5 rounded-full border px-2 py-1 transition-opacity focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary ${visible ? "bg-background" : "opacity-40 line-through"}`}
            >
              <span className="h-2.5 w-2.5 rounded-sm" style={{ backgroundColor: colorForSeries(series.name) }} />
              {series.name.replace(" US Equity", "")}
            </button>
          );
        })}
        {interaction.hiddenSeries.length > 0 && (
          <button
            type="button"
            onClick={() => dispatch({ type: "show_all" })}
            className="rounded-full px-2 py-1 font-semibold text-primary hover:bg-primary/10 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary"
          >
            Show all
          </button>
        )}
        <span className="ml-1 text-muted-foreground">Click a company to show or hide it</span>
      </div>
      <div className="relative overflow-x-auto">
        {tooltip && (
          <div className="pointer-events-none absolute right-3 top-3 z-10 rounded-md border bg-background/95 px-3 py-2 text-xs shadow-lg backdrop-blur" role="status" aria-live="polite">
            <div className="font-semibold">{tooltip.seriesName.replace(" US Equity", "")}</div>
            <div className="text-muted-foreground">{tooltip.year} · {formatMillions(tooltip.value)} USD mn</div>
          </div>
        )}
        <svg viewBox={`0 0 ${width} ${height}`} className="min-w-[900px]" role="img" aria-label="Grouped bar chart of hyperscaler and neocloud capex by year in USD millions">
          {ticks.map((tick) => {
            const tickY = y(tick);
            return (
              <g key={tick}>
                <line x1={margin.left} x2={width - margin.right} y1={tickY} y2={tickY} stroke="#e5e7eb" strokeWidth="1" />
                <text x={margin.left - 10} y={tickY + 4} textAnchor="end" fontSize="11" fill="#6b7280">
                  {Math.round(tick).toLocaleString()}
                </text>
              </g>
            );
          })}
          <line x1={margin.left} x2={width - margin.right} y1={zeroY} y2={zeroY} stroke="#374151" strokeWidth="1.5" />
          {model.years.map((year, yearIndex) => {
            const groupX = margin.left + yearIndex * groupWidth + groupPadding;
            return (
              <g key={`${year}-${yearIndex}`}>
                {model.series.map((series, seriesIndex) => {
                  const value = series.values[yearIndex];
                  if (value === null || !Number.isFinite(value)) return null;
                  const valueY = y(value);
                  const label = `${series.name.replace(" US Equity", "")}, ${year}, ${formatMillions(value)} USD million`;
                  return (
                    <rect
                      key={series.name}
                      x={groupX + seriesIndex * barWidth + 0.5}
                      y={Math.min(zeroY, valueY)}
                      width={Math.max(1, barWidth - 1)}
                      height={Math.max(1, Math.abs(valueY - zeroY))}
                      rx="1"
                      fill={colorForSeries(series.name)}
                      opacity={capexSeriesOpacity(series.name, interaction)}
                      className="cursor-pointer transition-opacity focus:outline-none"
                      tabIndex={0}
                      aria-label={label}
                      onPointerEnter={() => {
                        dispatch({ type: "highlight", seriesName: series.name });
                        setTooltip({ seriesName: series.name, year, value });
                      }}
                      onPointerLeave={clearHighlight}
                      onFocus={() => {
                        dispatch({ type: "highlight", seriesName: series.name });
                        setTooltip({ seriesName: series.name, year, value });
                      }}
                      onBlur={clearHighlight}
                    >
                      <title>{`${series.name} · ${year}: ${formatMillions(value)} USD mn`}</title>
                    </rect>
                  );
                })}
                <text x={margin.left + yearIndex * groupWidth + groupWidth / 2} y={height - margin.bottom + 24} textAnchor="middle" fontSize="12" fontWeight="600" fill="#374151">
                  {year}
                </text>
              </g>
            );
          })}
          <text x="18" y={margin.top + plotHeight / 2} textAnchor="middle" fontSize="12" fill="#4b5563" transform={`rotate(-90 18 ${margin.top + plotHeight / 2})`}>
            USD million
          </text>
          {model.series.length === 0 && (
            <text x={margin.left + plotWidth / 2} y={margin.top + plotHeight / 2} textAnchor="middle" fontSize="14" fill="#6b7280">
              Select a company above to display its data
            </text>
          )}
        </svg>
      </div>
    </section>
  );
}


export function ModelTab() {
  const [activeTab, setActiveTab] = useState<WorkbookTab>("hbm");
  const { data, error, isLoading, isFetching, refetch } = useQuery<WorkbookModelResponse>({
    queryKey: ["model", "workbook"],
    queryFn: api.getWorkbookModel,
    staleTime: 0,
    refetchOnWindowFocus: true,
  });
  const viewState = workbookViewState({ isLoading, error, hasData: Boolean(data) });

  return (
    <div className="mx-auto max-w-[1500px] px-6 py-6">
      <div className="mb-4 flex flex-wrap items-start justify-between gap-4">
        <div>
          <div className="flex items-center gap-2">
            <FileSpreadsheet className="h-5 w-5 text-primary" />
            <h2 className="text-lg font-bold">Memory Wafer Model</h2>
          </div>
          {data && (
            <p className="mt-1 text-xs text-muted-foreground">
              {data.source.filename} · Updated {new Date(data.source.modified_at).toLocaleString()}
            </p>
          )}
        </div>
        <button
          type="button"
          onClick={() => refetch()}
          disabled={isFetching}
          className="inline-flex items-center gap-1.5 rounded-md border bg-background px-3 py-1.5 text-sm font-semibold hover:bg-muted disabled:opacity-50"
        >
          {isFetching ? <Loader2 className="h-4 w-4 animate-spin" /> : <RefreshCw className="h-4 w-4" />}
          Refresh workbook
        </button>
      </div>

      <div className="mb-4 inline-flex rounded-md border p-0.5">
        {(["hbm", "capex"] as WorkbookTab[]).map((tab) => (
          <button
            key={tab}
            type="button"
            onClick={() => setActiveTab(tab)}
            className={`rounded px-4 py-1.5 text-sm font-medium transition-colors ${activeTab === tab ? "bg-primary text-primary-foreground" : "text-muted-foreground hover:bg-muted"}`}
          >
            {tab === "hbm" ? "HBM Model" : "Capex"}
          </button>
        ))}
      </div>

      {viewState === "loading" && (
        <div className="py-24 text-center text-muted-foreground">
          <Loader2 className="mx-auto mb-3 h-6 w-6 animate-spin" /> Reading workbook…
        </div>
      )}
      {viewState === "error" && (
        <div className="flex items-start gap-2 rounded-md border border-destructive/30 bg-destructive/5 px-4 py-3 text-sm text-destructive">
          <AlertCircle className="mt-0.5 h-4 w-4 shrink-0" />
          <span>Unable to read the Memory Model workbook: {String(error)}</span>
        </div>
      )}
      {viewState === "ready" && data && activeTab === "hbm" && (
        <WorkbookGrid view={buildHbmTableView(data.hbm)} />
      )}
      {viewState === "ready" && data && activeTab === "capex" && (
        <div className="space-y-5">
          <CapexBarChart capex={data.capex} />
          <WorkbookGrid view={buildCapexTableView(data.capex)} />
        </div>
      )}
    </div>
  );
}
