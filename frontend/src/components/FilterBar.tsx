import { useBrokers } from "../hooks/useCharts";
import { RefreshCw } from "lucide-react";

interface Filters {
  broker?: string;
  search?: string;
}

interface Props {
  filters: Filters;
  onChange: (f: Filters) => void;
  onRefresh?: () => void;
  isRefreshing?: boolean;
}

export function FilterBar({ filters, onChange, onRefresh, isRefreshing = false }: Props) {
  const { data: brokers } = useBrokers();

  return (
    <div className="flex items-center gap-3 flex-wrap">
      {/* Search */}
      <div className="relative flex-1 min-w-[200px] max-w-sm">
        <svg
          className="absolute left-3 top-1/2 -translate-y-1/2 h-4 w-4 text-muted-foreground"
          fill="none"
          viewBox="0 0 24 24"
          stroke="currentColor"
          strokeWidth={2}
        >
          <path
            strokeLinecap="round"
            strokeLinejoin="round"
            d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z"
          />
        </svg>
        <input
          type="text"
          placeholder="Search charts..."
          value={filters.search || ""}
          onChange={(e) =>
            onChange({ ...filters, search: e.target.value || undefined })
          }
          className="w-full rounded-md border bg-background pl-9 pr-3 py-2 text-sm placeholder:text-muted-foreground"
        />
      </div>

      {/* Broker filter */}
      <select
        value={filters.broker || ""}
        onChange={(e) =>
          onChange({ ...filters, broker: e.target.value || undefined })
        }
        className="rounded-md border bg-background px-3 py-2 text-sm"
      >
        <option value="">All brokers</option>
        {brokers?.map((b) => (
          <option key={b} value={b}>
            {b}
          </option>
        ))}
      </select>

      {onRefresh && (
        <button
          onClick={onRefresh}
          disabled={isRefreshing}
          className="inline-flex items-center gap-1.5 rounded-md border px-3 py-2 text-sm text-muted-foreground hover:bg-muted hover:text-foreground disabled:opacity-50"
          title="Refresh Gallery from local charts and ES RAG memory PDF snippets"
        >
          <RefreshCw className={`h-4 w-4 ${isRefreshing ? "animate-spin" : ""}`} />
          Refresh
        </button>
      )}

      {/* Clear */}
      {(filters.broker || filters.search) && (
        <button
          onClick={() => onChange({})}
          className="rounded-md px-3 py-2 text-sm text-muted-foreground hover:text-foreground hover:bg-muted transition-colors"
        >
          Clear
        </button>
      )}
    </div>
  );
}
