import { useState } from "react";
import { ChartGrid } from "./ChartGrid";
import type { CategoryGroup, ChartRecord } from "../lib/api";

const CATEGORY_COLORS: Record<string, string> = {
  DRAM: "bg-blue-100 text-blue-800",
  "DDR DRAM": "bg-indigo-100 text-indigo-800",
  HBM: "bg-purple-100 text-purple-800",
  NAND: "bg-amber-100 text-amber-800",
  "General Memory": "bg-gray-100 text-gray-800",
};

interface Props {
  group: CategoryGroup;
  onSelectChart: (chart: ChartRecord) => void;
}

export function CategorySection({ group, onSelectChart }: Props) {
  const [open, setOpen] = useState(false);
  const totalCharts = group.subcategories.reduce(
    (sum, s) => sum + s.charts.length,
    0
  );
  const badgeColor =
    CATEGORY_COLORS[group.category] || "bg-gray-100 text-gray-800";

  return (
    <div className="rounded-xl border bg-card">
      <button
        type="button"
        onClick={() => setOpen(!open)}
        aria-expanded={open}
        className="flex w-full items-center justify-between px-5 py-4 text-left hover:bg-muted/50 transition-colors rounded-xl"
      >
        <div className="flex items-center gap-3">
          <svg
            className={`h-4 w-4 text-muted-foreground transition-transform ${
              open ? "rotate-90" : ""
            }`}
            fill="none"
            viewBox="0 0 24 24"
            stroke="currentColor"
            strokeWidth={2}
          >
            <path
              strokeLinecap="round"
              strokeLinejoin="round"
              d="M9 5l7 7-7 7"
            />
          </svg>
          <h2 className="text-lg font-semibold">{group.category}</h2>
          <span
            className={`inline-flex items-center rounded-full px-2.5 py-0.5 text-xs font-medium ${badgeColor}`}
          >
            {totalCharts}
          </span>
        </div>
      </button>

      {open && (
        <div className="px-5 pb-5 space-y-5">
          {group.subcategories.map((sub) => (
            <div key={sub.subcategory}>
              <h3 className="text-sm font-medium text-muted-foreground mb-2 px-1">
                {sub.subcategory}{" "}
                <span className="text-xs">({sub.charts.length})</span>
              </h3>
              <ChartGrid charts={sub.charts} onSelect={onSelectChart} />
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
