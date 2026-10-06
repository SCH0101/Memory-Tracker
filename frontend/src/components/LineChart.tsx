import { useMemo, useState } from "react";
import type { DashSeries, SeriesPoint } from "../lib/api";
import { seriesHex, seriesLegendLabel, seriesStrokeDasharray } from "../lib/seriesColors";

interface Props {
  series: DashSeries[];
  unit: string;
  height?: number;
  legendMode?: "full" | "variant";
  /** Click handler for a data point - receives the series and the point. */
  onPointClick?: (series: DashSeries, point: SeriesPoint) => void;
}

interface HoverState {
  series: DashSeries;
  point: SeriesPoint;
  cx: number;  // svg coords
  cy: number;
}

const DISPLAY_YEAR_WINDOW = 10;

function fmtVal(v: number): string {
  const a = Math.abs(v);
  if (a >= 1000) return v.toLocaleString(undefined, { maximumFractionDigits: 0 });
  if (a >= 10) return v.toFixed(0);
  if (a >= 1) return v.toFixed(1);
  return v.toFixed(2);
}

export function LineChart({ series, unit, height = 220, legendMode = "full", onPointClick }: Props) {
  const W = 560;
  const H = height;
  const padL = 48;
  const padR = 12;
  const padT = 10;
  const padB = 46;

  // Per-broker hidden state (legend toggle). Names match DashSeries.broker.
  const [hidden, setHidden] = useState<Set<string>>(new Set());
  const [hover, setHover] = useState<HoverState | null>(null);
  const toggle = (name: string) =>
    setHidden((prev) => {
      const next = new Set(prev);
      if (next.has(name)) next.delete(name);
      else next.add(name);
      return next;
    });

  const windowedSeries = useMemo(() => {
    let latestYear = -Infinity;
    for (const s of series) {
      for (const p of s.points) {
        const y = Math.floor(p.period_int / 10000);
        if (Number.isFinite(y)) latestYear = Math.max(latestYear, y);
      }
    }
    if (!Number.isFinite(latestYear)) return series;
    const firstYear = latestYear - DISPLAY_YEAR_WINDOW + 1;
    return series
      .map((s) => ({
        ...s,
        points: s.points.filter((p) => {
          const y = Math.floor(p.period_int / 10000);
          return y >= firstYear && y <= latestYear;
        }),
      }))
      .filter((s) => s.points.length > 0);
  }, [series]);

  const visibleSeries = useMemo(
    () => windowedSeries.filter((s) => !hidden.has(s.broker)),
    [windowedSeries, hidden]
  );

  const model = useMemo(() => {
    // x categories = sorted unique period_int across VISIBLE series (so axis
    // collapses when you hide series, keeping the chart legible).
    const periodMap = new Map<number, string>();
    const sourceForBounds = visibleSeries.length ? visibleSeries : windowedSeries;
    for (const s of sourceForBounds)
      for (const p of s.points) periodMap.set(p.period_int, p.period);
    const cats = Array.from(periodMap.keys()).sort((a, b) => a - b);
    const catIndex = new Map(cats.map((c, i) => [c, i]));

    let yMin = Infinity;
    let yMax = -Infinity;
    for (const s of sourceForBounds)
      for (const p of s.points) {
        if (p.value < yMin) yMin = p.value;
        if (p.value > yMax) yMax = p.value;
      }
    if (!Number.isFinite(yMin)) {
      yMin = 0;
      yMax = 1;
    }
    if (yMin === yMax) {
      yMin -= 1;
      yMax += 1;
    }
    const span = yMax - yMin;
    yMax += span * 0.08;
    yMin -= span * 0.08;
    if (yMin > 0 && yMin < span * 0.5) yMin = 0;

    const labels = cats.map((c) => periodMap.get(c) || String(c));
    return { cats, catIndex, yMin, yMax, labels };
  }, [windowedSeries, visibleSeries]);

  const { cats, catIndex, yMin, yMax, labels } = model;
  const innerW = W - padL - padR;
  const innerH = H - padT - padB;

  const x = (period_int: number) => {
    const i = catIndex.get(period_int) ?? 0;
    if (cats.length <= 1) return padL + innerW / 2;
    return padL + (i / (cats.length - 1)) * innerW;
  };
  const y = (v: number) =>
    padT + innerH - ((v - yMin) / (yMax - yMin)) * innerH;

  const yTicks = 4;
  const gridY = Array.from({ length: yTicks + 1 }, (_, i) => {
    const v = yMin + ((yMax - yMin) * i) / yTicks;
    return { v, y: y(v) };
  });

  const labelStep = labels.length > 9 ? Math.ceil(labels.length / 8) : 1;

  const tip = hover
    ? (() => {
        const fn = hover.point.source?.filename || "";
        const pg = hover.point.source?.page || 0;
        const source = fn
          ? `${fn.split(/[\\/]/).pop() || fn}${pg > 0 ? ` p.${pg}` : ""}`
          : "No source";
        return { fn: source, pg: 0 };
      })()
    : null;
  const canOpenPdf = (p: SeriesPoint) =>
    !!p.source?.filename && !!p.source?.page && p.source.filename.toLowerCase().endsWith(".pdf");

  return (
    <div className="relative w-full" onMouseLeave={() => setHover(null)}>
      <svg viewBox={`0 0 ${W} ${H}`} className="w-full" role="img">
        {/* y grid + labels */}
        {gridY.map((g, i) => (
          <g key={i}>
            <line
              x1={padL}
              x2={W - padR}
              y1={g.y}
              y2={g.y}
              stroke="currentColor"
              className="text-border"
              strokeWidth={1}
              opacity={0.5}
            />
            <text
              x={padL - 6}
              y={g.y + 3}
              textAnchor="end"
              className="fill-muted-foreground"
              style={{ fontSize: 9 }}
            >
              {fmtVal(g.v)}
            </text>
          </g>
        ))}

        {/* x labels */}
        {cats.map((c, i) =>
          i % labelStep === 0 ? (
            <text
              key={c}
              x={x(c)}
              y={H - padB + 14}
              textAnchor="middle"
              className="fill-muted-foreground"
              style={{ fontSize: 9 }}
            >
              {labels[i]}
            </text>
          ) : null
        )}

        {/* visible series */}
        {visibleSeries.map((s) => {
          const color = seriesHex(s.broker);
          const strokeDasharray = seriesStrokeDasharray(s.broker);
          const pts = [...s.points].sort((a, b) => a.period_int - b.period_int);
          const d = pts
            .map((p, i) => `${i === 0 ? "M" : "L"}${x(p.period_int).toFixed(1)},${y(p.value).toFixed(1)}`)
            .join(" ");
          return (
            <g key={s.broker}>
              {pts.length > 1 && (
                <path
                  d={d}
                  fill="none"
                  stroke={color}
                  strokeWidth={1.75}
                  strokeDasharray={strokeDasharray}
                />
              )}
              {pts.map((p, i) => (
                <g key={i}>
                  <circle
                    cx={x(p.period_int)}
                    cy={y(p.value)}
                    r={pts.length === 1 ? 4 : 2.8}
                    fill={color}
                    stroke="#fff"
                    strokeWidth={1.2}
                    style={{ pointerEvents: "none" }}
                  />
                  <circle
                    cx={x(p.period_int)}
                    cy={y(p.value)}
                    r={8}
                    fill="transparent"
                    onMouseEnter={() => setHover({ series: s, point: p, cx: x(p.period_int), cy: y(p.value) })}
                    onClick={onPointClick ? () => onPointClick(s, p) : undefined}
                    style={canOpenPdf(p) ? { cursor: "pointer" } : undefined}
                  />
                  {hover && hover.series.broker === s.broker && hover.point.period_int === p.period_int && (
                    <circle
                      cx={x(p.period_int)}
                      cy={y(p.value)}
                      r={4.5}
                      fill={color}
                      stroke="#fff"
                      strokeWidth={1.5}
                      style={{ pointerEvents: "none" }}
                    />
                  )}
                </g>
              ))}
            </g>
          );
        })}
      </svg>

      {/* Hover value/source card. */}
      {tip && (
        <div className="pointer-events-none absolute right-2 top-2 z-20 w-56 max-w-[calc(100%-1rem)] rounded-md border border-slate-200 bg-white px-2.5 py-2 text-[11px] text-slate-900 shadow-lg">
          <div className="font-mono text-[12px] font-semibold">
            <span
              className="hidden"
              style={{ backgroundColor: seriesHex(hover!.series.broker) }}
            />
            <span className="hidden">{hover!.series.broker}</span>
            <span className="hidden">-</span>
            <span className="hidden">{hover!.point.period}</span>
            <span className="hidden">=</span>
            <span>
              {fmtVal(hover!.point.value)} {unit}
            </span>
          </div>
          <div className="flex flex-wrap items-center gap-x-2 text-[10px] text-muted-foreground">
            {tip.fn && <span className="truncate" title={tip.fn}>{tip.fn}</span>}
            {tip.pg > 0 && <span>- p.{tip.pg}</span>}
          </div>
        </div>
      )}

      {/* Toggleable legend */}
      <div className="mt-1 flex flex-wrap gap-x-3 gap-y-1 px-2">
        {windowedSeries.map((s) => {
          const isHidden = hidden.has(s.broker);
          return (
            <button
              key={s.broker}
              type="button"
              onClick={() => toggle(s.broker)}
              title={isHidden ? "Click to show" : "Click to hide"}
              className={`inline-flex items-center gap-1 text-[10px] transition-opacity ${
                isHidden ? "opacity-40 line-through" : "opacity-100"
              } text-muted-foreground hover:text-foreground`}
            >
              <span
                className="inline-block h-2 w-2 rounded-full"
                style={{ backgroundColor: seriesHex(s.broker) }}
              />
              {legendMode === "variant" ? seriesLegendLabel(s.broker) : s.broker}
            </button>
          );
        })}
      </div>
    </div>
  );
}
