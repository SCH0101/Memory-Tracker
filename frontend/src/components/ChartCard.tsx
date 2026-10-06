import type { ChartRecord } from "../lib/api";

interface Props {
  chart: ChartRecord;
  onClick: () => void;
}

const BROKER_COLORS: Record<string, string> = {
  UBS: "bg-red-100 text-red-700",
  GS: "bg-blue-100 text-blue-700",
  JPM: "bg-slate-100 text-slate-700",
  MS: "bg-emerald-100 text-emerald-700",
  BofA: "bg-rose-100 text-rose-700",
  BOFA: "bg-rose-100 text-rose-700",
  Citi: "bg-sky-100 text-sky-700",
  CITI: "bg-sky-100 text-sky-700",
  Barclays: "bg-cyan-100 text-cyan-700",
  BARC: "bg-cyan-100 text-cyan-700",
  DB: "bg-indigo-100 text-indigo-700",
  Deutsche: "bg-indigo-100 text-indigo-700",
  "Deutsche Bank": "bg-indigo-100 text-indigo-700",
  CLSA: "bg-orange-100 text-orange-700",
  Nomura: "bg-violet-100 text-violet-700",
  HSBC: "bg-red-100 text-red-700",
  TrendForce: "bg-teal-100 text-teal-700",
  Bern: "bg-lime-100 text-lime-700",
  Bernstein: "bg-lime-100 text-lime-700",
  Arete: "bg-amber-100 text-amber-800",
  "Arete Research": "bg-amber-100 text-amber-800",
};

function formatDate(dateInt: number | null): string {
  if (!dateInt) return "";
  const s = String(dateInt);
  if (s.length !== 8) return s;
  return `${s.slice(0, 4)}-${s.slice(4, 6)}-${s.slice(6, 8)}`;
}

export function ChartCard({ chart, onClick }: Props) {
  const brokerColor =
    BROKER_COLORS[chart.broker] || "bg-gray-100 text-gray-700";
  const imageSrc = chart.image_url || `/charts/${chart.image_path}`;

  return (
    <div
      onClick={onClick}
      className="flex-shrink-0 w-[260px] rounded-lg border bg-card overflow-hidden cursor-pointer hover:shadow-md hover:border-primary/30 transition-all group"
    >
      <div className="relative h-[170px] bg-muted overflow-hidden">
        <img
          src={imageSrc}
          alt={chart.tag || "Chart"}
          className="w-full h-full object-contain group-hover:scale-105 transition-transform"
          loading="lazy"
        />
      </div>
      <div className="p-2.5">
        <p className="text-xs font-medium leading-tight line-clamp-2 mb-1.5">
          {chart.tag || chart.caption || `Chart p${chart.page_number}`}
        </p>
        <div className="flex items-center gap-1.5 flex-wrap">
          {chart.broker && (
            <span
              className={`inline-flex items-center rounded px-1.5 py-0.5 text-[10px] font-medium ${brokerColor}`}
            >
              {chart.broker}
            </span>
          )}
          {chart.chart_date_int && (
            <span className="text-[10px] text-muted-foreground">
              {formatDate(chart.chart_date_int)}
            </span>
          )}
          {chart.is_rag && (
            <span className="text-[10px] rounded bg-teal-50 px-1.5 py-0.5 font-medium text-teal-700">
              ES RAG
            </span>
          )}
        </div>
      </div>
    </div>
  );
}
